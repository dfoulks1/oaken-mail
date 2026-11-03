# SPDX-License-Identifier: Apache-2.0
"""Action operations (ADR 0019): what the actions did about a message, and a yes for one that waits
for the person. Neither marks mail read, and the results carry no message text: a status, the name
of an error type, and counts."""

from pydantic import BaseModel, Field

from oaken.core.errors import NotFound
from oaken.core.ids import MessageId
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation


class GetActionInput(BaseModel):
    message_id: str


class ActionRow(BaseModel):
    action: str
    route: str
    status: str = Field(description="approved, done, failed or skipped.")
    version: str = ""
    error: str = Field(default="", description="The name of the error type, never its message.")
    attempts: int = 0
    updated_at: str = ""


class GetActionOutput(BaseModel):
    message_id: str
    actions: list[ActionRow]


@operation(
    "get_action",
    "Show what the actions did about a message: for each action and route, whether it succeeded, "
    "failed or was skipped. An action that is waiting for approval is a parked job.",
    permission="read",
    input=GetActionInput,
    output=GetActionOutput,
    mcp="on",
)
async def get_action(ctx: OpContext, inp: GetActionInput) -> GetActionOutput:
    mid = MessageId(inp.message_id)
    await ctx.svc.raw.get_message(mid)  # NotFound if there is no such message
    rows = await ctx.svc.need_pipeline().action_rows(mid)
    return GetActionOutput(
        message_id=inp.message_id, actions=[ActionRow.model_validate(r) for r in rows]
    )


class ApproveActionInput(BaseModel):
    message_id: str
    action: str = Field(description="The action plugin's name.")
    route: str


class ApproveActionOutput(BaseModel):
    approved: bool


@operation(
    "approve_action",
    "Say yes to an action that is waiting for approval: it runs now, for this message and route.",
    permission="mutate",
    input=ApproveActionInput,
    output=ApproveActionOutput,
    mcp="never",  # an agent never says yes for the person
)
async def approve_action(ctx: OpContext, inp: ApproveActionInput) -> ApproveActionOutput:
    mid = MessageId(inp.message_id)
    await ctx.svc.raw.get_message(mid)
    if not await ctx.svc.need_pipeline().approve_action(mid, inp.action, inp.route):
        raise NotFound(f"no action {inp.action!r} is waiting on route {inp.route!r} for that message")
    return ApproveActionOutput(approved=True)


class ListActionsInput(BaseModel):
    limit: int = Field(default=25, ge=1, le=100)


class WaitingAction(BaseModel):
    kind: str = Field(description="The action's name, or `unrouted` when no action answers.")
    message_id: str
    route: str
    why: str = ""


class RecentAction(ActionRow):
    message_id: str


class ListActionsOutput(BaseModel):
    waiting: list[WaitingAction]
    recent: list[RecentAction]


@operation(
    "list_actions",
    "The actions waiting for approval or for a route to be answered, and what the actions did "
    "lately. No message text.",
    permission="read",
    input=ListActionsInput,
    output=ListActionsOutput,
    mcp="off",  # the person turns it on
)
async def list_actions(ctx: OpContext, inp: ListActionsInput) -> ListActionsOutput:
    pipeline = ctx.svc.need_pipeline()
    return ListActionsOutput(
        waiting=[WaitingAction.model_validate(w) for w in await pipeline.waiting_actions()],
        recent=[
            RecentAction.model_validate(r) for r in await pipeline.recent_actions(inp.limit)
        ],
    )


OPERATIONS: tuple[AnyOperation, ...] = (get_action, approve_action, list_actions)
