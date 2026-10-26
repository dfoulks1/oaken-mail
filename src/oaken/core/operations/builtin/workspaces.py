# SPDX-License-Identifier: Apache-2.0
"""Workspace management: list, get, check, create/update, and remove workspaces.

Workspaces are named confidentiality levels (prompt 10): each has a trust level (``secure`` or
``insecure``) and an LLM backend choice. Their LLM caches and usage are kept apart. A ``secure``
workspace only ever uses a backend that reports ``locality = "local"``.

All five operations are ``mcp="never"``. A workspace change reaches the running core immediately
(live reload via ``oaken.core.settings``) and does not require a restart.
"""

import contextlib
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from oaken.ai.workspaces import DEFAULT_WORKSPACE, Workspace, Workspaces, check_backend
from oaken.core import config_edit
from oaken.core.errors import ConfigError, InvalidInput, NotFound, PermissionDenied, StorageError
from oaken.core.operations.builtin.config import _config_text, _write
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation
from oaken.core.settings import Settings, WorkspaceSettings

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

# The TOML table path prefix for a workspace: ["workspaces", name]
_WS_PREFIX = "workspaces"


def _ws_path(name: str) -> list[str]:
    return [_WS_PREFIX, name]


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise InvalidInput(f"workspace name {name!r} is invalid: use 1-32 of a-z, 0-9, '-' and '_'")


def _current_settings() -> Settings:
    return Settings.load()


def _workspace_toml(name: str, settings: Settings) -> str:
    """Serialise the workspace as a minimal TOML snippet (no header)."""
    ws = settings.workspaces.get(name)
    if ws is None:
        return ""
    lines = [f'trust = "{ws.trust}"']
    if ws.llm_backend:
        lines.append(f'llm_backend = "{ws.llm_backend}"')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Usage helpers
# ---------------------------------------------------------------------------


async def _usage_for(ctx: OpContext, name: str) -> "WorkspaceUsage":
    """Aggregate ai_usage rows for one workspace."""
    usage = ctx.svc.usage
    if usage is None:
        return WorkspaceUsage()
    rows = await usage.usage_summary(workspace=name)
    calls = sum(r.calls for r in rows)
    tokens_in = sum(r.tokens_in for r in rows)
    tokens_out = sum(r.tokens_out for r in rows)
    cache_entries = await _count_cache(ctx, name)
    return WorkspaceUsage(
        calls=calls,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_entries=cache_entries,
    )


async def _count_cache(ctx: OpContext, name: str) -> int | None:
    """Cached answers for ``name``, or None when there is no cache to ask yet (no table). Any other
    failure surfaces: a wrong number shown as the truth is worse than none."""
    try:
        return (await ctx.svc.cache().stats(workspace=name)).entries
    except StorageError:
        return None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class WorkspaceUsage(BaseModel):
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_entries: int | None = None  # None: the cache could not say


class WorkspaceInfo(BaseModel):
    name: str
    trust: str  # "secure" | "insecure"
    llm_backend: str  # entry-point name actually in use (resolved from ai.llm_backend when empty)
    is_default: bool
    usage: WorkspaceUsage = Field(default_factory=WorkspaceUsage)


class ListWorkspacesInput(BaseModel):
    pass


class ListWorkspacesOutput(BaseModel):
    workspaces: list[WorkspaceInfo]


class WorkspaceRef(BaseModel):
    name: str


class GetWorkspaceOutput(BaseModel):
    name: str
    trust: str
    llm_backend: str  # the effective backend (resolved)
    llm_backend_raw: str  # as stored in config (may be "" meaning "use ai.llm_backend")
    toml: str  # the table's settings as editable TOML text (no header)
    overridden_keys: list[str] = Field(
        default_factory=list,
        description="Keys whose values are overridden by an environment variable.",
    )
    usage: WorkspaceUsage = Field(default_factory=WorkspaceUsage)


class WorkspaceCandidate(BaseModel):
    name: str
    toml: str = Field(
        default="",
        description="The workspace's settings as TOML text (no header). "
        "An empty string leaves the existing settings unchanged.",
    )


class FieldError(BaseModel):
    key: str
    message: str


class CheckWorkspaceOutput(BaseModel):
    valid: bool
    errors: list[FieldError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    effective_backend: str = ""  # what would actually run, after the check


class SetWorkspaceOutput(BaseModel):
    ok: bool
    name: str
    path: str = ""
    backup: str | None = None
    error: str | None = None
    errors: list[FieldError] = Field(default_factory=list)  # which field, when a check refused


class RemoveWorkspaceOutput(BaseModel):
    removed: bool
    name: str
    path: str = ""
    backup: str | None = None
    cache_kept: bool = Field(
        default=True,
        description="LLM cache and usage rows are kept; clear them with cache_clear.",
    )
    error: str | None = None


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@operation(
    "list_workspaces",
    "List every configured workspace plus the implicit 'default', with its trust level, "
    "effective LLM backend, and aggregate usage numbers.",
    permission="read",
    input=ListWorkspacesInput,
    output=ListWorkspacesOutput,
    mcp="off",
)
async def list_workspaces(ctx: OpContext, inp: ListWorkspacesInput) -> ListWorkspacesOutput:
    settings = _current_settings()
    ws_obj = Workspaces(settings)
    out: list[WorkspaceInfo] = []
    for name in ws_obj.names():
        ws = ws_obj.get(name)
        usage = await _usage_for(ctx, name)
        out.append(
            WorkspaceInfo(
                name=name,
                trust=ws.trust,
                llm_backend=ws.llm_backend,
                is_default=(name == DEFAULT_WORKSPACE),
                usage=usage,
            )
        )
    return ListWorkspacesOutput(workspaces=out)


@operation(
    "get_workspace",
    "One workspace: its settings as editable TOML, the effective LLM backend, and usage numbers.",
    permission="read",
    input=WorkspaceRef,
    output=GetWorkspaceOutput,
    mcp="off",
)
async def get_workspace(ctx: OpContext, inp: WorkspaceRef) -> GetWorkspaceOutput:
    settings = _current_settings()
    ws_obj = Workspaces(settings)
    ws = ws_obj.get(inp.name)  # raises ConfigError (NotFound) if absent
    raw_cfg = settings.workspaces.get(inp.name) or WorkspaceSettings()
    usage = await _usage_for(ctx, inp.name)
    toml = (
        _workspace_toml(inp.name, settings)
        if inp.name in settings.workspaces
        else (f'trust = "{raw_cfg.trust}"\n')
    )
    return GetWorkspaceOutput(
        name=inp.name,
        trust=ws.trust,
        llm_backend=ws.llm_backend,
        llm_backend_raw=raw_cfg.llm_backend,
        toml=toml,
        usage=usage,
    )


@operation(
    "check_workspace",
    "Validate workspace settings without saving: field errors, the effective backend, and a "
    "warning when trust would drop from secure to insecure.",
    permission="read",
    input=WorkspaceCandidate,
    output=CheckWorkspaceOutput,
    mcp="never",
)
async def check_workspace(ctx: OpContext, inp: WorkspaceCandidate) -> CheckWorkspaceOutput:
    v = _verdict(ctx, inp.name, inp.toml)
    return CheckWorkspaceOutput(
        valid=not v.errors,
        errors=v.errors,
        warnings=v.warnings,
        effective_backend=v.effective_backend,
    )


@operation(
    "set_workspace",
    "Create or update a workspace in config.toml, keeping all other tables and comments. "
    "The change takes effect immediately (live reload); no restart needed.",
    permission="mutate",
    input=WorkspaceCandidate,
    output=SetWorkspaceOutput,
    mcp="never",
)
async def set_workspace(ctx: OpContext, inp: WorkspaceCandidate) -> SetWorkspaceOutput:
    v = _verdict(ctx, inp.name, inp.toml)
    if v.errors:  # the same gate as check_workspace: a refused workspace is never written
        return SetWorkspaceOutput(
            ok=False,
            name=inp.name,
            error="; ".join(f"{e.key}: {e.message}" for e in v.errors),
            errors=v.errors,
        )

    text = _config_text()
    try:
        new_text = config_edit.set_table(text, _ws_path(inp.name), v.values)
    except ConfigError as exc:
        return SetWorkspaceOutput(ok=False, name=inp.name, error=str(exc))

    result = _write(ctx, new_text)
    if not result.ok:
        return SetWorkspaceOutput(ok=False, name=inp.name, error=result.error)

    # live reload: rebuild the Workspaces object from the freshly written settings
    _reload_workspaces(ctx)

    return SetWorkspaceOutput(ok=True, name=inp.name, path=result.path, backup=result.backup)


@operation(
    "remove_workspace",
    "Remove a workspace from config.toml. Refuses to remove 'default'. "
    "LLM cache and usage rows are kept; clear them separately.",
    permission="mutate",
    input=WorkspaceRef,
    output=RemoveWorkspaceOutput,
    mcp="never",
)
async def remove_workspace(ctx: OpContext, inp: WorkspaceRef) -> RemoveWorkspaceOutput:
    if inp.name == DEFAULT_WORKSPACE:
        return RemoveWorkspaceOutput(
            removed=False,
            name=inp.name,
            error="the 'default' workspace cannot be removed",
        )

    settings = _current_settings()
    if inp.name not in settings.workspaces:
        raise NotFound(f"no workspace named {inp.name!r}")

    text = _config_text()
    new_text = config_edit.remove_table(text, _ws_path(inp.name))
    result = _write(ctx, new_text)
    if not result.ok:
        return RemoveWorkspaceOutput(removed=False, name=inp.name, error=result.error)

    _reload_workspaces(ctx)

    return RemoveWorkspaceOutput(
        removed=True, name=inp.name, path=result.path, backup=result.backup
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _Verdict:
    values: dict[str, Any] = field(default_factory=dict)
    effective_backend: str = ""
    errors: list[FieldError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _verdict(ctx: OpContext, name: str, toml: str) -> _Verdict:
    """The one check ``check_workspace`` reports and ``set_workspace`` obeys: the name, the shape of
    the settings, and (for a secure workspace) that its effective backend is local."""
    v = _Verdict()
    try:
        _validate_name(name)
    except InvalidInput as exc:
        v.errors.append(FieldError(key="name", message=str(exc)))
        return v

    try:  # snippet_values takes bare keys or a headed table
        v.values = config_edit.snippet_values(toml, _ws_path(name)) if toml.strip() else {}
    except ConfigError as exc:
        v.errors.append(FieldError(key="toml", message=str(exc)))
        return v

    try:
        ws_cfg = WorkspaceSettings.model_validate(v.values)
    except ValidationError as exc:
        for e in exc.errors(include_input=False, include_url=False):
            key = str(e["loc"][0]) if e.get("loc") else "toml"
            v.errors.append(FieldError(key=key, message=e["msg"]))
        return v

    settings = _current_settings()
    v.effective_backend = ws_cfg.llm_backend or settings.ai.llm_backend

    # confidentiality rule: a secure workspace needs a local backend
    if ws_cfg.trust == "secure":
        backend = _find_backend(ctx, v.effective_backend)
        if backend is not None:
            try:
                check_backend(Workspace(name, ws_cfg.trust, v.effective_backend), backend)
            except PermissionDenied as exc:
                v.errors.append(FieldError(key="llm_backend", message=str(exc)))

    # warn on secure -> insecure downgrade
    existing = settings.workspaces.get(name)
    if existing and existing.trust == "secure" and ws_cfg.trust == "insecure":
        v.warnings.append(
            f"changing {name!r} from secure to insecure: a remote model may now see mail "
            "text from accounts assigned to this workspace; caches remain separate"
        )
    return v


def _find_backend(ctx: OpContext, backend_name: str) -> object | None:
    """Return the started LLM backend instance by name, or None if unavailable."""
    plugins = ctx.svc.plugins
    if plugins is None:
        return None
    try:
        return plugins.llm_backend(backend_name)
    except Exception:  # noqa: BLE001
        return None


def _reload_workspaces(ctx: OpContext) -> None:
    """Rebuild the in-process Workspaces object from the freshly written settings."""
    with contextlib.suppress(Exception):  # best-effort; the file was already validated
        new_settings = Settings.load()
        if ctx.svc.llm is not None:
            ctx.svc.llm.workspaces = Workspaces(new_settings)


OPERATIONS: tuple[AnyOperation, ...] = (
    list_workspaces,
    get_workspace,
    check_workspace,
    set_workspace,
    remove_workspace,
)
