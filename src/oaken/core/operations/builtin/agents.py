# SPDX-License-Identifier: Apache-2.0
"""Operations about external agents (the swarm): create one, list them, revoke one.

All three are the person's alone: an agent that could mint another agent, or un-revoke itself, would
widen its own reach, so none is ever offered over MCP. The token is returned once, by
``create_agent``, and is never stored or shown again (only its hash is kept). An agent then uses it
on the agent routes (``/v1/agent/...``), which no other token opens. See the swarm page.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from oaken.core.agents import Agent
from oaken.core.ids import AccountId, AgentId
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation


class AgentInfo(BaseModel):
    id: str
    label: str
    kinds: list[str]
    account_ids: list[str] | None  # None: every account
    max_leases: int
    max_per_hour: int
    created_at: datetime
    last_seen_at: datetime | None
    revoked: bool

    @classmethod
    def of(cls, a: Agent) -> "AgentInfo":
        return cls(
            id=str(a.id),
            label=a.label,
            kinds=sorted(a.kinds),
            account_ids=sorted(a.account_ids) if a.account_ids is not None else None,
            max_leases=a.max_leases,
            max_per_hour=a.max_per_hour,
            created_at=a.created_at,
            last_seen_at=a.last_seen_at,
            revoked=a.revoked,
        )


class CreateAgentInput(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    kinds: list[str] = Field(min_length=1, max_length=32)
    account_ids: list[str] | None = None
    max_leases: int | None = Field(default=None, ge=1)
    max_per_hour: int | None = Field(default=None, ge=1)


class CreateAgentOutput(BaseModel):
    agent: AgentInfo
    token: str = Field(description="Shown once. Give it to the agent; it cannot be recovered.")


@operation(
    "create_agent",
    "Register an external agent and show its token once.",
    permission="mutate",
    input=CreateAgentInput,
    output=CreateAgentOutput,
    mcp="never",  # an agent must not be able to mint another
)
async def create_agent(ctx: OpContext, inp: CreateAgentInput) -> CreateAgentOutput:
    """The swarm must be enabled and every kind listed in ``[swarm] allowed_kinds``."""
    agent, token = await ctx.svc.need_agents().create(
        inp.label,
        inp.kinds,
        account_ids=[AccountId(a) for a in inp.account_ids]
        if inp.account_ids is not None
        else None,
        max_leases=inp.max_leases,
        max_per_hour=inp.max_per_hour,
    )
    return CreateAgentOutput(agent=AgentInfo.of(agent), token=token)


class ListAgentsInput(BaseModel):
    include_revoked: bool = False


class ListAgentsOutput(BaseModel):
    agents: list[AgentInfo]


@operation(
    "list_agents",
    "List the registered agents (never their tokens).",
    permission="read",
    input=ListAgentsInput,
    output=ListAgentsOutput,
    mcp="never",
)
async def list_agents(ctx: OpContext, inp: ListAgentsInput) -> ListAgentsOutput:
    found = await ctx.svc.need_agents().list(include_revoked=inp.include_revoked)
    return ListAgentsOutput(agents=[AgentInfo.of(a) for a in found])


class RevokeAgentInput(BaseModel):
    agent_id: str


class RevokeAgentOutput(BaseModel):
    revoked: bool  # False: it was already revoked


@operation(
    "revoke_agent",
    "Revoke an agent at once; the jobs it holds go back to the queue.",
    permission="mutate",
    input=RevokeAgentInput,
    output=RevokeAgentOutput,
    mcp="never",
)
async def revoke_agent(ctx: OpContext, inp: RevokeAgentInput) -> RevokeAgentOutput:
    return RevokeAgentOutput(revoked=await ctx.svc.need_agents().revoke(AgentId(inp.agent_id)))


OPERATIONS: tuple[AnyOperation, ...] = (create_agent, list_agents, revoke_agent)
