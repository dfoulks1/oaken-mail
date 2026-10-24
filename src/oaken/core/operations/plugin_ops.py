# SPDX-License-Identifier: Apache-2.0
"""Turn what plugins offer into registry operations.

A plugin declares ``PluginOperation`` values; this namespaces each as ``<plugin>.<name>`` and wraps
it so the same rules apply as to a core operation. The plugin never sees the databases, never
marks mail read, and cannot be on over MCP until the user turns it on. A plugin's own errors are
reduced to their type: a failure inside a plugin can quote the mail it was handling.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from oaken.core.errors import InvalidInput, OakenError, PluginError
from oaken.core.logging import get_logger
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import (
    PLUGIN_SEP,
    AnyOperation,
    Change,
    Operation,
    OperationDefinitionError,
    Preview,
)
from oaken.plugins.host import OfferedOperation, PluginHost

log = get_logger("oaken.operations")


def adapt(host: PluginHost, offered: OfferedOperation) -> AnyOperation:
    plugin, d = offered.plugin, offered.declaration
    timeout = host.call_timeout

    def pctx(ctx: OpContext) -> Any:
        return host.operation_context(
            plugin, ctx.caller.client, ctx.caller.actor.label or ctx.caller.actor.kind
        )

    async def resolve(ctx: OpContext, inp: BaseModel) -> tuple[Any, BaseModel]:
        """The handler to call and the input to call it with. A ``per_account`` command runs on
        the instance serving that account, with ``account_id`` filled in (an empty one means the
        only account this plugin has)."""
        if not d.per_account:
            return d.handler, inp
        account = str(getattr(inp, "account_id", "") or "")
        if not account:
            mine = [a.id for a in await ctx.svc.raw.list_accounts() if a.provider == plugin]
            if len(mine) != 1:
                raise InvalidInput(
                    f"no {plugin} account; add one with `oaken admin account add`"
                    if not mine
                    else f"several {plugin} accounts ({', '.join(map(str, mine))}); name one"
                    " with --account-id"
                )
            account = str(mine[0])
            inp = inp.model_copy(update={"account_id": account})
        instance = host.account_instance(plugin, account)
        bound = getattr(instance, getattr(d.handler, "__name__", ""), None)
        if not callable(bound):
            raise PluginError(f"{plugin}.{d.name} is not available on account {account!r}")
        return bound, inp

    async def handler(ctx: OpContext, inp: BaseModel) -> BaseModel:
        try:
            fn, inp = await resolve(ctx, inp)
            context = (
                host.account_context(
                    plugin,
                    str(getattr(inp, "account_id", "")),
                    ctx.caller.client,
                    ctx.caller.actor.label or ctx.caller.actor.kind,
                )
                if d.per_account
                else pctx(ctx)
            )
            result = await asyncio.wait_for(fn(context, inp), timeout)
            return d.output.model_validate(result)
        except OakenError:
            raise
        except Exception as exc:
            raise PluginError(f"{plugin}.{d.name} failed ({type(exc).__name__})") from None

    async def preview(ctx: OpContext, inp: BaseModel) -> Preview:
        if d.preview is None:
            return Preview(summary=f"Run {plugin}.{d.name}: {d.summary}")
        try:
            p = await asyncio.wait_for(d.preview(pctx(ctx), inp), timeout)
        except OakenError:
            raise
        except Exception as exc:
            raise PluginError(f"{plugin}.{d.name} preview failed ({type(exc).__name__})") from None
        return Preview(
            summary=p.summary,
            changes=[
                Change(target=c.target, field=c.field, before=c.before, after=c.after)
                for c in p.changes
            ],
            total_changes=p.total,
        )

    return Operation(
        name=f"{plugin}{PLUGIN_SEP}{d.name}",
        summary=d.summary,
        permission=d.permission,
        input=d.input,
        output=d.output,
        handler=handler,
        mcp=d.mcp,
        marks_read=False,
        preview=preview if d.permission == "mutate" else None,
        audit="registry",
        plugin=plugin,
        description=f"Offered by the {plugin} plugin.",
    )


def plugin_operations(host: PluginHost) -> Sequence[AnyOperation]:
    """The operations the healthy plugins offer now. One that breaks a rule is left out (logged)."""
    out: list[AnyOperation] = []
    for offered in host.offered_operations():
        try:
            out.append(adapt(host, offered))
        except OperationDefinitionError as exc:
            log.error("plugin operation refused", plugin=offered.plugin, problem=str(exc))
    return out
