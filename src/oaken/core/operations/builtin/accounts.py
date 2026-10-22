# SPDX-License-Identifier: Apache-2.0
"""Operations that set up an account: create it, change its settings, store its credential, check
that the credential works, and run an interactive consent (Gmail).

None is ever offered over MCP. Only non-secret settings go in an account's config (the store
refuses keys that look like secrets). A credential goes straight from here to the provider, which
keeps it in the OS keyring; it is never stored, logged, audited or returned. The audit row for a
password change holds the account and provider only, not a hash of the input (a hash of a short
password can be guessed).
"""

from typing import Any

from pydantic import BaseModel, Field, SecretStr

from oaken.core.errors import InvalidInput, NotFound
from oaken.core.ids import AccountId
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation
from oaken.mail.sendpolicy import effective_capabilities, is_receive_only
from oaken.plugins.base import inferred_capabilities
from oaken.readstate.audit import new_audit_id


class AccountDetail(BaseModel):
    account_id: str
    provider: str
    display_name: str
    config: dict[str, Any]
    receive_only: bool = Field(
        default=False,
        description="Listen but never send: core refuses to route a send for this account.",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="What the account offers (auth, sync.pull, flags.write, send), with 'send' "
        "dropped when it is receive-only.",
    )
    problem: str | None = Field(default=None, description="Why it cannot sync now, if it cannot.")
    config_schema: dict[str, Any] | None = None


def _account_capabilities(ctx: OpContext, account_id: str, config: dict[str, Any]) -> list[str]:
    """The account's effective capabilities: its provider's, minus 'send' when receive-only.
    Reads them from the started provider instance if there is one, so the tags match what the
    host would actually route; an account with no healthy provider reports none."""
    accounts = ctx.svc.need_sync().accounts
    try:
        provider = accounts.get(account_id, allow_unhealthy=True)
    except Exception:  # noqa: BLE001 - no provider started yet: no capabilities to report
        return []
    inferred = inferred_capabilities(provider, "provider")
    return effective_capabilities(inferred, config)


async def _detail(ctx: OpContext, account_id: str) -> AccountDetail:
    acct = await ctx.svc.raw.get_account(AccountId(account_id))
    host = ctx.svc.need_plugins()
    accounts = ctx.svc.need_sync().accounts
    return AccountDetail(
        account_id=acct.id,
        provider=acct.provider,
        display_name=acct.display_name,
        config=acct.config,
        receive_only=is_receive_only(acct.config),
        capabilities=_account_capabilities(ctx, acct.id, acct.config),
        problem=accounts.problem(acct.id),
        config_schema=host.config_schema("provider", acct.provider),
    )


class AccountRef(BaseModel):
    account_id: str


@operation(
    "get_account",
    "One account: provider, its non-secret settings, what the provider's settings are, and why it "
    "cannot sync if it cannot.",
    permission="read",
    input=AccountRef,
    output=AccountDetail,
    mcp="never",
)
async def get_account(ctx: OpContext, inp: AccountRef) -> AccountDetail:
    return await _detail(ctx, inp.account_id)


class CreateAccountInput(BaseModel):
    account_id: str = Field(description="1-32 characters of a-z, 0-9, '-' and '_'.")
    provider: str
    display_name: str
    config: dict[str, Any] = Field(default_factory=dict, description="Non-secret settings only.")


@operation(
    "create_account",
    "Add an account. Its credential is stored separately with set_account_password or "
    "start_authorization.",
    permission="mutate",
    input=CreateAccountInput,
    output=AccountDetail,
    mcp="never",
)
async def create_account(ctx: OpContext, inp: CreateAccountInput) -> AccountDetail:
    if inp.provider not in {
        s.name for s in ctx.svc.need_plugins().statuses() if s.kind == "provider"
    }:
        raise InvalidInput(f"no provider named {inp.provider!r} is installed")
    await ctx.svc.raw.create_account(inp.account_id, inp.provider, inp.display_name, inp.config)
    await ctx.svc.need_sync().accounts.start()  # a provider instance for the new account
    return await _detail(ctx, inp.account_id)


class SetConfigInput(BaseModel):
    account_id: str
    config: dict[str, Any] = Field(description="The whole non-secret config; replaces the old.")
    display_name: str | None = Field(
        default=None, description="A new display name for the account, if given."
    )


class SetConfigOutput(BaseModel):
    account: AccountDetail
    restart_needed: bool = Field(
        description="The running provider keeps its old settings until core restarts."
    )


@operation(
    "set_account_config",
    "Replace an account's non-secret settings (and, if given, its display name).",
    permission="mutate",
    input=SetConfigInput,
    output=SetConfigOutput,
    mcp="never",
)
async def set_account_config(ctx: OpContext, inp: SetConfigInput) -> SetConfigOutput:
    await ctx.svc.raw.set_account_config(AccountId(inp.account_id), inp.config)
    if inp.display_name is not None:
        await ctx.svc.raw.set_account_display_name(AccountId(inp.account_id), inp.display_name)
    return SetConfigOutput(account=await _detail(ctx, inp.account_id), restart_needed=True)


class PasswordInput(BaseModel):
    account_id: str
    password: SecretStr = Field(min_length=1, max_length=1024)


class PasswordOutput(BaseModel):
    stored: bool


@operation(
    "set_account_password",
    "Store an account's password in the OS keyring (SMTP and IMAP). It is never shown again.",
    permission="mutate",
    input=PasswordInput,
    output=PasswordOutput,
    mcp="never",
    audit="service",
)
async def set_account_password(ctx: OpContext, inp: PasswordInput) -> PasswordOutput:
    provider = ctx.svc.need_sync().accounts.get(inp.account_id)
    setter = getattr(provider, "set_password", None)
    if setter is None:
        raise InvalidInput("this account's provider does not use a password")
    await setter(inp.password.get_secret_value())
    acct = await ctx.svc.raw.get_account(AccountId(inp.account_id))
    async with ctx.svc.raw.db.transaction() as tx:
        await ctx.svc.audit.record(
            tx,
            audit_id=new_audit_id(),
            actor=ctx.caller.actor,
            reason=f"{ctx.caller.client}_call",
            op="account_password_set",
            batch_id=ctx.caller.request_id,
            before={},
            after={"account_id": inp.account_id, "provider": acct.provider},
        )
    return PasswordOutput(stored=True)


class CheckOutput(BaseModel):
    ok: bool
    detail: str = ""


@operation(
    "check_account",
    "Check the account's credential: connect, require TLS and log in. Sends and reads nothing.",
    permission="read",
    input=AccountRef,
    output=CheckOutput,
    mcp="never",
)
async def check_account(ctx: OpContext, inp: AccountRef) -> CheckOutput:
    from oaken.core.errors import OakenError

    provider = ctx.svc.need_sync().accounts.get(inp.account_id)
    try:
        await provider.authenticate(provider.ctx)  # type: ignore[attr-defined]
    except OakenError as exc:
        return CheckOutput(ok=False, detail=exc.message)
    return CheckOutput(ok=True)


class AuthorizationStart(BaseModel):
    url: str = Field(description="Open this in a browser; core waits for the redirect back.")


@operation(
    "start_authorization",
    "Begin an interactive consent (Gmail). Returns the address to open; poll authorization_status.",
    permission="mutate",
    input=AccountRef,
    output=AuthorizationStart,
    mcp="never",
)
async def start_authorization(ctx: OpContext, inp: AccountRef) -> AuthorizationStart:
    accounts = ctx.svc.need_sync().accounts
    provider = accounts.get(inp.account_id, allow_unhealthy=True)  # a dead credential is why
    if not callable(getattr(provider, "authorize", None)):
        raise InvalidInput("this account's provider has no interactive authorization")
    return AuthorizationStart(url=await accounts.authorizations.start(inp.account_id, provider))


class AuthorizationStatus(BaseModel):
    state: str = Field(description="none, pending, done or failed.")
    detail: str = ""


@operation(
    "authorization_status",
    "How an interactive authorization is going.",
    permission="read",
    input=AccountRef,
    output=AuthorizationStatus,
    mcp="never",
)
async def authorization_status(ctx: OpContext, inp: AccountRef) -> AuthorizationStatus:
    await ctx.svc.raw.get_account(AccountId(inp.account_id))  # NotFound for a typo
    state, detail = ctx.svc.need_sync().accounts.authorizations.status(inp.account_id)
    if state == "none" and inp.account_id not in {a.id for a in await ctx.svc.raw.list_accounts()}:
        raise NotFound(f"no such account: {inp.account_id}")
    return AuthorizationStatus(state=state, detail=detail)


class RemoveAccountOutput(BaseModel):
    removed: bool
    schedules_removed: int = Field(
        default=0, description="How many sync schedules the account had, now gone."
    )
    restart_needed: bool = Field(
        default=False,
        description="A started provider instance keeps serving until core restarts; it is no "
        "longer offered for a new sync.",
    )
    mail_kept: bool = Field(
        default=True,
        description="Stored messages, raw bytes and attributes are kept (purge is a separate op).",
    )


@operation(
    "remove_account",
    "Remove an account and its sync schedules. Stored mail is kept; the credential is dropped.",
    permission="mutate",
    input=AccountRef,
    output=RemoveAccountOutput,
    mcp="never",
)
async def remove_account(ctx: OpContext, inp: AccountRef) -> RemoveAccountOutput:
    account_id = AccountId(inp.account_id)
    await ctx.svc.raw.get_account(account_id)  # NotFound for a typo, before any change
    sync = ctx.svc.need_sync()
    schedules_removed = 0
    for sched in await sync.schedules():
        if sched.account_id == inp.account_id:
            if await sync.remove(sched.account_id, sched.folder, actor=ctx.caller.actor):
                schedules_removed += 1
    removed = await ctx.svc.raw.delete_account(account_id)
    sync.accounts.forget(inp.account_id)
    return RemoveAccountOutput(
        removed=removed,
        schedules_removed=schedules_removed,
        restart_needed=True,
    )


OPERATIONS: tuple[AnyOperation, ...] = (
    get_account,
    create_account,
    set_account_config,
    set_account_password,
    check_account,
    remove_account,
    start_authorization,
    authorization_status,
)
