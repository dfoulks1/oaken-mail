# SPDX-License-Identifier: Apache-2.0
"""Optional base classes that make writing a plugin short.

Subclass the one for your kind, set a few attributes and override only what your plugin does.
Everything else has a harmless default, and the capabilities of a provider are worked out from
the methods you override, so there is no second list to keep in step with the code.

    class MyEmbedder(BaseEmbedder):
        model_id = "my-model"
        dimensions = 384

        async def embed(self, texts):
            return [my_model(t) for t in texts]

Nothing requires these classes: any object with the members of a contract works, and the host
checks the same rules either way (``oaken.plugins.validate``).
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel

from oaken.core.errors import InvalidInput, OakenError
from oaken.plugins.adapt import to_enrich_output
from oaken.plugins.authorize import Authorizations
from oaken.plugins.contracts import (
    CAPABILITIES,
    PLUGIN_API_VERSION,
    EnrichInput,
    EnrichOutput,
    Health,
    Hit,
    LLMRequest,
    LLMResponse,
    PluginContext,
    PluginKind,
    PluginOpContext,
    PluginOperation,
    PullBatch,
    VectorItem,
    VectorStats,
)
from oaken.plugins.provider_auth import AuthIn, AuthOut, AuthStatusOut, PasswordAuthIn

F = TypeVar("F", bound=Callable[..., Any])


# Set by the host on a started instance whose ``[plugins.<name>]`` table says ``capabilities =
# [...]``: the user's allow-list, which can only narrow what the plugin implements.
ALLOWED_ATTR = "_oaken_allowed"


def _default(fn: F) -> F:
    """Mark a base-class method as a placeholder: overriding it is what implements it."""
    fn._oaken_default = True  # type: ignore[attr-defined]
    return fn


def is_implemented(obj: object, method: str) -> bool:
    """True if ``obj`` has a callable ``method`` that is not a base-class placeholder."""
    fn = getattr(obj, method, None)
    return callable(fn) and not getattr(fn, "_oaken_default", False)


def inferred_capabilities(obj: object, kind: str) -> frozenset[str]:
    """The capabilities of ``kind`` whose method ``obj`` really implements."""
    caps = frozenset(c for c, m in CAPABILITIES[kind].items() if is_implemented(obj, m))
    allowed: frozenset[str] | None = getattr(obj, ALLOWED_ATTR, None)
    return caps if allowed is None else caps & allowed


def command(
    name: str,
    summary: str,
    *,
    input: type[BaseModel],
    output: type[BaseModel],
    permission: Literal["read", "mutate"] = "read",
    preview: str | None = None,
    mcp: Literal["off", "never"] = "off",
    per_account: bool = False,
) -> Callable[[F], F]:
    """Offer a method of your plugin as a command: an operation core registers as
    ``<plugin>.<name>``, reachable over the API and, because the CLI builds its verbs from the
    operation's input schema, as ``oaken <plugin> <name>`` with one flag per input field.

        class MyProvider(BaseProvider):
            @command("login", "Sign in to the service.", permission="mutate",
                     input=LoginIn, output=LoginOut)
            async def login(self, ctx: PluginOpContext, inp: LoginIn) -> LoginOut: ...

    ``per_account=True`` runs it on the instance of the account named by the input's
    ``account_id`` (see ``PluginOperation``). ``preview`` names another method (same signature,
    returning ``PluginPreview``) that says what a ``mutate`` command would change. The rules of
    ``PluginOperation`` apply: a plugin cannot send, and a command is never on over MCP until the
    user turns it on.
    """

    def wrap(fn: F) -> F:
        fn._oaken_command = {  # type: ignore[attr-defined]
            "name": name,
            "summary": summary,
            "input": input,
            "output": output,
            "permission": permission,
            "preview": preview,
            "mcp": mcp,
            "per_account": per_account,
        }
        return fn

    return wrap


class _Base:
    kind: ClassVar[PluginKind]
    api_version: ClassVar[int] = PLUGIN_API_VERSION
    version: ClassVar[str] = ""
    # Shown where people set the plugin up (the getting-started page, ``list_plugins``): a few
    # plain sentences on what to do first, and a ``config.toml`` snippet to start from. Core
    # knows nothing about any one plugin, so what is specific to it is said here.
    setup_help: ClassVar[str] = ""
    config_example: ClassVar[str] = ""
    # Whether the plugin runs when its table does not say ``enabled``. A plugin that should be
    # opt-in (it costs money, or reaches a network) sets this False; ``enabled = true`` turns it on.
    enabled_default: ClassVar[bool] = True
    ctx: PluginContext  # set by start()

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        return None

    async def health(self) -> Health:
        return Health(True)

    def operations(self) -> list[PluginOperation]:
        """The methods marked with ``@command``. Override to add operations by hand, and call
        ``super().operations()`` to keep these."""
        found: list[PluginOperation] = []
        for attr in sorted(dir(type(self))):
            spec = getattr(getattr(type(self), attr, None), "_oaken_command", None)
            if spec is None:
                continue
            preview = getattr(self, spec["preview"]) if spec["preview"] else None
            found.append(
                PluginOperation(
                    name=spec["name"],
                    summary=spec["summary"],
                    permission=spec["permission"],
                    input=spec["input"],
                    output=spec["output"],
                    handler=getattr(self, attr),
                    preview=preview,
                    mcp=spec["mcp"],
                    per_account=spec["per_account"],
                )
            )
        return found


class BaseProvider(_Base):
    """Override ``pull`` (a mail source) or ``send`` (a sender) at least. Add ``authenticate`` and
    ``set_flags`` as the service allows.

    Capabilities come from what you override (``pull`` is ``sync.pull``, ``set_flags`` is
    ``flags.write``). To state them yourself, set ``capabilities = frozenset({...})``; the host
    rejects a capability whose method you did not write.
    """

    kind: ClassVar[PluginKind] = "provider"
    # True if ``pull(cursor, folder=...)`` can sync one folder alone (IMAP). Gmail has labels, not
    # folders, and says False.
    supports_folders: ClassVar[bool] = False
    # Non-secret settings a new account of this provider starts with (``{"receive_only": True}``
    # makes it listen but not send until the person turns that on). Clients read them from
    # ``list_plugins``; nothing in core names a provider.
    account_defaults: ClassVar[dict[str, Any]] = {}

    @_default
    async def authenticate(self, ctx: PluginContext) -> None:
        raise NotImplementedError

    @_default
    def pull(self, cursor: str | None, folder: str | None = None) -> AsyncIterator[PullBatch]:
        raise NotImplementedError

    @_default
    async def set_flags(
        self, provider_ids: Sequence[str], add: Collection[str], remove: Collection[str]
    ) -> None:
        raise NotImplementedError

    @_default
    async def send(self, raw_bytes: bytes) -> str:
        raise NotImplementedError

    # -- sign-in, offered as ``<plugin> auth`` when the provider can --------------------------
    # Implement ``set_password(password)`` (a secret you type) or ``authorize(show_url)`` (a
    # consent in the browser) and the provider offers ``auth`` with no further code; ``--check``
    # tries the stored credential through ``authenticate``. See ``oaken.plugins.provider_auth``.
    def operations(self) -> list[PluginOperation]:
        allowed: frozenset[str] | None = getattr(self, ALLOWED_ATTR, None)
        signs_in = allowed is None or "auth" in allowed  # the user's allow-list can switch it off
        secret = signs_in and callable(getattr(self, "set_password", None))
        consent = signs_in and callable(getattr(self, "authorize", None))
        keep = {"_auth_secret": secret and not consent, "_auth_consent": consent}
        keep["_auth_status"] = consent
        return [
            op for op in super().operations() if keep.get(getattr(op.handler, "__name__", ""), True)
        ]

    async def stop(self) -> None:
        runs: Authorizations | None = self.__dict__.get("_authorizations")
        if runs is not None:  # a sign-in still waiting for the person ends with the provider
            await runs.close()
        await super().stop()

    @staticmethod
    def _why(exc: Exception) -> str:
        return exc.message if isinstance(exc, OakenError) else type(exc).__name__

    async def _check_login(self, account: str, done: str) -> AuthOut:
        try:
            await self.authenticate(self.ctx)
        except Exception as exc:  # noqa: BLE001 - reported to the person, not raised
            return AuthOut(account_id=account, state="failed", detail=self._why(exc))
        return AuthOut(account_id=account, state="done", detail=done)

    @command(
        "auth",
        "Store this account's password in the OS keyring and check the login.",
        permission="mutate",
        input=PasswordAuthIn,
        output=AuthOut,
        per_account=True,
    )
    async def _auth_secret(self, ctx: PluginOpContext, inp: PasswordAuthIn) -> AuthOut:
        if inp.check:
            return await self._check_login(inp.account_id, "login works")
        password = inp.password.get_secret_value()
        if not password:
            raise InvalidInput("no password given")
        await self.set_password(password)  # type: ignore[attr-defined]
        return await self._check_login(
            inp.account_id, "password stored in the OS keyring; login works"
        )

    @command(
        "auth",
        "Sign in through the provider's consent page. Returns the address to open.",
        permission="mutate",
        input=AuthIn,
        output=AuthOut,
        per_account=True,
    )
    async def _auth_consent(self, ctx: PluginOpContext, inp: AuthIn) -> AuthOut:
        if inp.check:
            return await self._check_login(inp.account_id, "the stored credential works")
        runs: Authorizations = self.__dict__.setdefault("_authorizations", Authorizations())
        url = await runs.start(inp.account_id, self, wait_for_url=20.0)
        plugin = self.ctx.name.partition("@")[0]
        return AuthOut(
            account_id=inp.account_id,
            state="pending",
            detail="open the address and approve; this finishes when the provider redirects back",
            open_url=url,
            poll=f"{plugin}.auth_status",
            poll_input={"account_id": inp.account_id},
        )

    @command(
        "auth_status",
        "How a sign-in that is waiting for the person is going.",
        input=AuthIn,
        output=AuthStatusOut,
        per_account=True,
    )
    async def _auth_status(self, ctx: PluginOpContext, inp: AuthIn) -> AuthStatusOut:
        runs: Authorizations = self.__dict__.setdefault("_authorizations", Authorizations())
        state, detail = runs.status(inp.account_id)
        if state == "done" and not detail:
            detail = "signed in; the credential is stored in the OS keyring"
        return AuthStatusOut(account_id=inp.account_id, state=state, detail=detail)


class BaseEmbedder(_Base):
    kind: ClassVar[PluginKind] = "embedder"
    model_id: str = ""
    dimensions: int = 0
    max_input_tokens: int = 8192

    @_default
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError


class BaseLLMBackend(_Base):
    kind: ClassVar[PluginKind] = "llm_backend"
    model_id: str = ""
    context_window: int = 0
    # Where the model runs: "local" (this machine) or "remote". A ``secure`` workspace refuses a
    # backend that does not say "local", so a plugin that forgets this is treated as remote.
    locality: str = "remote"

    @_default
    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError


class BaseEnricher(_Base):
    """Set ``version`` and, ideally, ``output_model`` (a pydantic model): the host then checks the
    result against it and publishes its JSON schema. Override ``enrich``."""

    kind: ClassVar[PluginKind] = "enricher"
    version: ClassVar[str] = "1"
    needs_llm: bool = False
    output_model: ClassVar[type[BaseModel] | None] = None

    @property
    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema() if self.output_model else {}

    @_default
    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        raise NotImplementedError


class BaseVectorStore(_Base):
    kind: ClassVar[PluginKind] = "vector_store"

    @_default
    async def upsert(self, items: Sequence[VectorItem]) -> None:
        raise NotImplementedError

    @_default
    async def delete(self, message_ids: Sequence[str]) -> None:
        raise NotImplementedError

    @_default
    async def query(
        self, vector: Sequence[float], k: int, filters: dict[str, str] | None = None
    ) -> list[Hit]:
        raise NotImplementedError

    @_default
    async def stats(self) -> VectorStats:
        raise NotImplementedError


def enricher(
    name: str,
    *,
    version: str = "1",
    needs_llm: bool = False,
    output: type[BaseModel] | None = None,
) -> Callable[[Callable[[EnrichInput], Awaitable[object]]], type[BaseEnricher]]:
    """Turn one async function into an enricher class, ready to register as an entry point:

        class Result(BaseModel):
            words: int

        @enricher("word-count", output=Result)
        async def word_count(msg: EnrichInput) -> Result:
            return Result(words=len(msg.body.unwrap().split()))

    The function may return the model, a dict or an ``EnrichOutput``; the host coerces it.
    """

    def wrap(fn: Callable[[EnrichInput], Awaitable[object]]) -> type[BaseEnricher]:
        async def enrich(self: BaseEnricher, msg: EnrichInput) -> EnrichOutput:
            return to_enrich_output(await fn(msg), name, output)

        return type(
            fn.__name__,
            (BaseEnricher,),
            {
                "name": name,
                "version": version,
                "needs_llm": needs_llm,
                "output_model": output,
                "enrich": enrich,
                "__doc__": fn.__doc__,
                "__module__": fn.__module__,
            },
        )

    return wrap
