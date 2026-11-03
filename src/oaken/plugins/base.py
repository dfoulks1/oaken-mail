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

import functools
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

from oaken.core.errors import InvalidInput, OakenError, PluginError
from oaken.core.triage import BUILTIN_LEVELS
from oaken.core.trace import brief, trace_context
from oaken.plugins.adapt import to_enrich_output
from oaken.plugins.authorize import Authorizations
from oaken.plugins.contracts import (
    CAPABILITIES,
    PLUGIN_API_VERSION,
    ActionResult,
    CacheEntry,
    CacheStats,
    CleanMessage,
    EnrichOutput,
    Health,
    Hit,
    LLMRequest,
    LLMResponse,
    PluginContext,
    PluginKind,
    PluginOpContext,
    PluginOperation,
    PluginTable,
    UiSpec,
    PullBatch,
    TriageVerdict,
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


# Not traced: ``health`` is polled on a timer and ``stop`` says nothing a person tunes by.
_NOT_TRACED = frozenset({"health", "stop"})


def _traced_call(name: str, fn: Any) -> Any:
    @functools.wraps(fn)
    async def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = getattr(self, "ctx", None)
        if ctx is None or not ctx.tracing():
            return await fn(self, *args, **kwargs)
        started = time.monotonic()
        with trace_context(plugin=ctx.name, plugin_kind=str(ctx.kind)):
            ctx.trace("plugin.call", method=name, args=brief(args), kwargs=brief(kwargs))
            try:
                result = await fn(self, *args, **kwargs)
            except Exception as exc:
                ctx.trace(
                    "plugin.error", method=name, error=type(exc).__name__,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )  # fmt: skip
                raise
            ctx.trace(
                "plugin.return", method=name, result=brief(result),
                duration_ms=round((time.monotonic() - started) * 1000),
            )  # fmt: skip
            return result

    call._oaken_traced = True  # type: ignore[attr-defined]
    return call


def _traced_stream(name: str, fn: Any) -> Any:
    @functools.wraps(fn)
    async def stream(self: Any, *args: Any, **kwargs: Any) -> Any:
        ctx = getattr(self, "ctx", None)
        if ctx is None or not ctx.tracing():
            async for item in fn(self, *args, **kwargs):
                yield item
            return
        started = time.monotonic()
        count = 0
        with trace_context(plugin=ctx.name, plugin_kind=str(ctx.kind)):
            ctx.trace("plugin.call", method=name, args=brief(args), kwargs=brief(kwargs))
            try:
                async for item in fn(self, *args, **kwargs):
                    count += 1
                    yield item
            except Exception as exc:
                ctx.trace(
                    "plugin.error", method=name, error=type(exc).__name__, items=count,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )  # fmt: skip
                raise
            ctx.trace(
                "plugin.return", method=name, items=count,
                duration_ms=round((time.monotonic() - started) * 1000),
            )  # fmt: skip

    stream._oaken_traced = True  # type: ignore[attr-defined]
    return stream


class BasePlugin:
    kind: ClassVar[PluginKind]
    api_version: ClassVar[int] = PLUGIN_API_VERSION
    version: ClassVar[str] = ""
    # Shown where people set the plugin up (the getting-started page, ``list_plugins``): a few
    # plain sentences on what to do first, and a ``config.toml`` snippet to start from. Core
    # knows nothing about any one plugin, so what is specific to it is said here.
    setup_help: ClassVar[str] = ""
    config_example: ClassVar[str] = ""
    # A pydantic model of this plugin's ``[plugins.<name>]`` settings. Declare it and ``start``
    # parses the table into ``self.config`` (a typed, validated instance), and the host publishes
    # its JSON Schema (``host.config_schema``) so the console can render a settings form. Leave it
    # None and ``self.config`` is None; the plugin reads ``ctx.config`` (a dict) itself.
    config_model: ClassVar[type[BaseModel] | None] = None
    # Whether the plugin runs when its table does not say ``enabled``. A plugin that should be
    # opt-in (it costs money, or reaches a network) sets this False; ``enabled = true`` turns it on.
    enabled_default: ClassVar[bool] = True
    # Settings the plugin cannot run without (``("base_url",)``). While its table lacks any of them
    # the host leaves it switched off, with the reason shown, and starts it when they are set.
    requires_config: ClassVar[tuple[str, ...]] = ()
    # What the plugin owns (ADR 0016): tables core creates for it as ``<kind>_<name>_<table>``
    # (``ctx.store``), and folders core provisions under ``OAKEN_HOME/plugins/<kind>/<name>/``
    # (``ctx.folders``). Declare nothing and the plugin has neither.
    tables: ClassVar[tuple[PluginTable, ...]] = ()
    folders: ClassVar[tuple[str, ...]] = ()
    def ui(self) -> UiSpec | None:
        """The plugin's web UI as data (ADR 0018): settings sections, buttons and pages. None: the
        settings modal is built from the config schema alone. Optional, additive."""
        return None

    ctx: PluginContext  # set by start()
    config: Any = None  # the parsed config_model instance, set by start() when config_model is set

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Every public ``async`` method a plugin defines records its call and return in the trace
        (when it is on), under this plugin's kind and name, so ``oaken trace -f
        plugin.<kind>.<name>`` shows what core asked of it and what it answered. A method written
        on a base class is wrapped once, where it is defined. Nothing to do in a plugin."""
        super().__init_subclass__(**kwargs)
        for name, fn in list(vars(cls).items()):
            if name.startswith("_") or name in _NOT_TRACED or getattr(fn, "_oaken_traced", False):
                continue
            if inspect.isasyncgenfunction(fn):
                setattr(cls, name, _traced_stream(name, fn))
            elif inspect.iscoroutinefunction(fn):
                setattr(cls, name, _traced_call(name, fn))

    def trace(self, kind: str, /, **data: Any) -> None:
        """Record an event of your own in the trace (``oaken trace -f plugin.<kind>.<name>``): a
        decision, a request you made, whatever you want to see while you build a plugin. Does
        nothing unless tracing is on, and before ``start``."""
        ctx = getattr(self, "ctx", None)
        if ctx is not None:
            ctx.trace(kind, **data)

    async def start(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        if self.config_model is not None:
            try:
                self.config = self.config_model.model_validate(ctx.config)
            except ValidationError as exc:
                fields = ", ".join(str(e["loc"][0]) for e in exc.errors() if e["loc"])
                raise PluginError(
                    f"{ctx.name}: invalid [plugins.{ctx.kind}.{ctx.name}] settings ({fields})"
                ) from None

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


_Base = BasePlugin  # the name before it was public


class BaseProvider(BasePlugin):
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


class BaseEmbedder(BasePlugin):
    kind: ClassVar[PluginKind] = "embedder"
    model_id: str = ""
    dimensions: int = 0
    max_input_tokens: int = 8192

    @_default
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError


class BaseLLMBackend(BasePlugin):
    kind: ClassVar[PluginKind] = "llm_backend"
    model_id: str = ""
    context_window: int = 0
    # Where the model runs: "local" (this machine) or "remote". A ``secure`` workspace refuses a
    # backend that does not say "local", so a plugin that forgets this is treated as remote.
    locality: str = "remote"

    def thinking_for(self, request: LLMRequest) -> bool | None:
        """Whether to let the model reason on this call, or None to leave it to the model. A
        ``think`` in the plugin's own settings wins over what the request asks for. A backend that
        cannot reason, or has no switch for it, need not call this: the field is optional."""
        chosen = getattr(self.config, "think", None) if self.config is not None else None
        return chosen if isinstance(chosen, bool) else request.think

    @_default
    async def generate(self, request: LLMRequest, *, model: str | None = None) -> LLMResponse:
        raise NotImplementedError

    async def available_models(self) -> list[str]:
        """The model ids this backend can run right now, for a picker to offer. The default is the
        one model it was started with; a backend that can enumerate (Ollama's ``/api/tags``)
        overrides this to list what is installed. Offer only what is available: never a name the
        backend cannot serve."""
        return [self.model_id] if self.model_id else []


class BaseAnalyzer(BasePlugin):
    """What the plugins that read a message and return a result share (ADR 0017): an enricher, a
    triager and an action. It is a helper for their base classes, not a kind of its own: it has no
    entry-point group and nothing outside the plugin classes names it. A plugin extends exactly one
    of ``BaseEnricher``, ``BaseTriager`` or ``BaseAction``.

    ``version`` is part of the identity of a stored result: bump it when the output could change.
    ``needs_llm`` says whether it asks a model. ``output_model`` is the pydantic model its result
    must match; the host checks it and publishes its JSON schema."""

    version: ClassVar[str] = "1"
    needs_llm: bool = False
    output_model: ClassVar[type[BaseModel] | None] = None

    @property
    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema() if self.output_model else {}


class BaseEnricher(BaseAnalyzer):
    """Set ``version`` and, ideally, ``output_model`` (a pydantic model): the host then checks the
    result against it and publishes its JSON schema. Override ``enrich``."""

    kind: ClassVar[PluginKind] = "enricher"

    @_default
    async def enrich(self, msg: CleanMessage) -> EnrichOutput:
        raise NotImplementedError


class LevelsIn(BaseModel):
    pass


class Level(BaseModel):
    value: int
    name: str
    builtin: bool


class LevelsOut(BaseModel):
    levels: list[Level]


class LevelSetIn(BaseModel):
    value: int = Field(ge=4, le=99)
    name: str = Field(min_length=1, max_length=40)


class LevelRemoveIn(BaseModel):
    value: int = Field(ge=4, le=99)


class BaseTriager(BaseAnalyzer):
    """The first step of the pipeline: rate a message so core can decide what else it is owed.
    This is the deterministic kind: override ``triage`` to return the verdict (a
    ``TriageVerdict``), or None to leave the message to the next triager. One that asks a model
    extends ``LLMTriager``. Triagers run in ``rank`` order (lowest first) and the first verdict
    wins, so the model is asked only for what the cheaper ones leave. Set ``output_model`` as for
    an enricher."""

    kind: ClassVar[PluginKind] = "triager"
    rank: ClassVar[int] = 50
    # The levels (a number and a name; 0 to 3 are built in, the reader's own start at 4) are one set
    # for every triager, coordinated here (ADR 0016): a table of the kind, not of one plugin.
    kind_tables: ClassVar[tuple[PluginTable, ...]] = (
        PluginTable("levels", {"value": "int", "name": "text"}),
    )

    async def level_table(self) -> dict[int, str]:
        """Every level now defined, by number: the built-in ones and the reader's own."""
        store = self.ctx.store
        custom = await store.find("levels", order_by="value") if store else []
        return {**BUILTIN_LEVELS, **{int(r["value"]): str(r["name"]) for r in custom}}

    def operations(self) -> list[PluginOperation]:
        """The level operations, offered by every triager over the one shared set."""
        return [
            PluginOperation("levels", "List the triage levels: built in and the reader's own.",
                            "read", LevelsIn, LevelsOut, self._levels),
            PluginOperation("level_set", "Add a triage level (4 to 99), or rename one of yours.",
                            "mutate", LevelSetIn, LevelsOut, self._level_set),
            PluginOperation("level_remove", "Remove one of your triage levels.",
                            "mutate", LevelRemoveIn, LevelsOut, self._level_remove),
        ]  # fmt: skip

    async def _levels(self, ctx: PluginOpContext, inp: "LevelsIn") -> "LevelsOut":
        return self._out(await self.level_table())

    @staticmethod
    def _out(table: dict[int, str]) -> "LevelsOut":
        return LevelsOut(levels=[Level(value=v, name=n, builtin=v in BUILTIN_LEVELS)
                                 for v, n in sorted(table.items())])  # fmt: skip

    async def _level_set(self, ctx: PluginOpContext, inp: "LevelSetIn") -> "LevelsOut":
        store = ctx.store
        if store is None:
            raise PluginError("levels need the plugin store")
        if inp.value in BUILTIN_LEVELS:
            raise PluginError(f"level {inp.value} is built in")
        have = await store.find("levels", {"value": inp.value})
        if have:
            await store.update("levels", have[0]["uid"], {"name": inp.name.strip()})
        else:
            await store.insert("levels", {"value": inp.value, "name": inp.name.strip()})
        return self._out(await self.level_table())

    async def _level_remove(self, ctx: PluginOpContext, inp: "LevelRemoveIn") -> "LevelsOut":
        store = ctx.store
        if store is None:
            raise PluginError("levels need the plugin store")
        if inp.value in BUILTIN_LEVELS:
            raise PluginError(f"level {inp.value} is built in")
        for row in await store.find("levels", {"value": inp.value}):
            await store.delete("levels", row["uid"])
        return self._out(await self.level_table())

    @_default
    async def triage(self, msg: CleanMessage) -> TriageVerdict | None:
        raise NotImplementedError

    def describe(self) -> list[dict[str, Any]]:
        """The steps this triager runs, in order, for the settings window: each a dict with a
        ``name``, a one-line ``summary`` and whether it is ``enabled``. Empty by default; a
        triager made of several checks (the rules) lists them."""
        return []


class BaseAction(BaseAnalyzer):
    """Does something about a message triage routed to it (ADR 0019). Set ``routes`` to the route
    names it answers to and override ``act``. Return an ``ActionResult`` (or None when there was
    nothing to do); raise to fail. It should be idempotent: a retry after a crash can run it again
    for the same message and route. Whether it runs on its own or waits for the person is the
    reader's setting, ``approval = "ask" | "auto"`` in its table, and the default is ``ask``."""

    kind: ClassVar[PluginKind] = "action"
    routes: ClassVar[tuple[str, ...]] = ()
    # Routes that run without waiting for the person unless the reader sets ``approval = "ask"``
    # in the plugin's table. Only for work that is safe to do unasked (a private copy).
    auto_routes: ClassVar[tuple[str, ...]] = ()

    @_default
    async def act(self, msg: CleanMessage, route: str) -> ActionResult | None:
        raise NotImplementedError


class BaseVectorStore(BasePlugin):
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


class BaseCache(BasePlugin):
    """The LLM answer cache. Override all five members; ``key`` may return any tuple of strings."""

    kind: ClassVar[PluginKind] = "cache_store"

    @_default
    def key(
        self, *, workspace: str, task: str, content_hash: str, model: str, prompt_version: str
    ) -> tuple[str, ...]:
        raise NotImplementedError

    @_default
    async def get(self, key: tuple[str, ...]) -> CacheEntry | None:
        raise NotImplementedError

    @_default
    async def put(self, key: tuple[str, ...], entry: CacheEntry) -> None:
        raise NotImplementedError

    @_default
    async def stats(self, workspace: str | None = None) -> CacheStats:
        raise NotImplementedError

    @_default
    async def clear(self, workspace: str | None = None, task: str | None = None) -> int:
        raise NotImplementedError


def enricher(
    name: str,
    *,
    version: str = "1",
    needs_llm: bool = False,
    output: type[BaseModel] | None = None,
) -> Callable[[Callable[[CleanMessage], Awaitable[object]]], type[BaseEnricher]]:
    """Turn one async function into an enricher class, ready to register as an entry point:

        class Result(BaseModel):
            words: int

        @enricher("word-count", output=Result)
        async def word_count(msg: CleanMessage) -> Result:
            return Result(words=len(msg.body.unwrap().split()))

    The function may return the model, a dict or an ``EnrichOutput``; the host coerces it.
    """

    def wrap(fn: Callable[[CleanMessage], Awaitable[object]]) -> type[BaseEnricher]:
        async def enrich(self: BaseEnricher, msg: CleanMessage) -> EnrichOutput:
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
