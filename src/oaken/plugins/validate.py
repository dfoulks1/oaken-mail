# SPDX-License-Identifier: Apache-2.0
"""Check a plugin against its contract. Used by the host at load time and by the contract suites.

Returns a list of problems in plain words rather than raising on the first one, so a plugin
author sees everything to fix at once.
"""

import re

from pydantic import BaseModel

from oaken.plugins.base import inferred_capabilities, is_implemented
from oaken.plugins.contracts import (
    CAPABILITIES,
    PLUGIN_API_VERSION,
    REQUIRED_MEMBERS,
    PluginInfo,
    PluginJobHandler,
    PluginKind,
    PluginOperation,
    PluginStage,
)

_OP_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

# Values a plugin may only learn in ``start()`` (the model it was configured with, the vector size
# a probe returned), so the host checks them after starting and not on the fresh instance.
STARTED_VALUES = frozenset({"model_id", "dimensions", "context_window"})


def declared_capabilities(obj: object, kind: str) -> frozenset[str]:
    """What the plugin claims: its ``capabilities`` attribute if it sets one, otherwise what its
    overridden methods imply."""
    explicit = getattr(obj, "capabilities", None)
    if explicit is None:
        return inferred_capabilities(obj, kind)
    try:
        return frozenset(explicit)
    except TypeError:
        return frozenset({f"<capabilities is not a collection: {explicit!r}>"})


def check_plugin(
    kind: PluginKind, name: str, obj: object, *, defer_started: bool = False
) -> tuple[PluginInfo | None, list[str]]:
    """The ``PluginInfo`` for ``obj`` (None if it cannot be built) and every problem found.

    With ``defer_started`` the ``STARTED_VALUES`` are only checked to exist; ``check_started``
    checks what they hold once the plugin has started."""
    problems: list[str] = []
    known = CAPABILITIES[kind]

    version = getattr(obj, "api_version", PLUGIN_API_VERSION)
    if version != PLUGIN_API_VERSION:
        problems.append(f"api_version is {version!r}, this core speaks {PLUGIN_API_VERSION}")

    caps = declared_capabilities(obj, kind)
    for cap in sorted(caps):
        if cap not in known:
            allowed = ", ".join(sorted(known)) or "none"
            problems.append(f"unknown capability {cap!r} for a {kind} (allowed: {allowed})")
        elif not is_implemented(obj, known[cap]):
            problems.append(f"declares {cap!r} but does not implement {known[cap]}()")
    if kind == "provider" and not caps & {"sync.pull", "send"}:
        problems.append(
            "a provider must implement pull() (capability 'sync.pull') "
            "or send() (capability 'send')"
        )

    for member in REQUIRED_MEMBERS[kind]:
        if not hasattr(obj, member):
            problems.append(f"missing {member}")
        elif callable(getattr(obj, member)) is False:
            if not (defer_started and member in STARTED_VALUES):
                problems.extend(_check_value(kind, obj, member))
        elif not is_implemented(obj, member):
            problems.append(f"{member}() is not implemented")

    problems.extend(check_operations(obj))
    problems.extend(check_job_handlers(obj))
    problems.extend(check_pipeline_stages(obj))

    ver = str(getattr(obj, "version", "") or "")
    info = PluginInfo(
        name=name,
        version=ver,
        kind=kind,
        capabilities=frozenset(c for c in caps if c in known),
        api_version=PLUGIN_API_VERSION,
    )
    return (info if not problems else None), problems


def check_started(kind: PluginKind, obj: object) -> list[str]:
    """Problems with the ``STARTED_VALUES`` of a plugin that has just started."""
    problems: list[str] = []
    for member in REQUIRED_MEMBERS[kind]:
        if member in STARTED_VALUES and hasattr(obj, member):
            problems.extend(_check_value(kind, obj, member))
    return problems


def _check_value(kind: str, obj: object, member: str) -> list[str]:
    v = getattr(obj, member)
    if member in ("model_id", "version"):
        return [] if isinstance(v, str) and v else [f"{member} must be a non-empty string"]
    if member in ("dimensions", "context_window"):
        ok = isinstance(v, int) and not isinstance(v, bool) and v > 0
        return [] if ok else [f"{member} must be a positive integer"]
    if member == "needs_llm":
        return [] if isinstance(v, bool) else ["needs_llm must be True or False"]
    return []


def declared_operations(obj: object) -> list[PluginOperation]:
    """The operations ``obj.operations()`` returns (none if it has no such method). Raises what the
    method raises; ``check_operations`` turns that into a problem."""
    fn = getattr(obj, "operations", None)
    if not callable(fn):
        return []
    return list(fn())


def check_operations(obj: object) -> list[str]:
    """Problems with the operations a plugin offers, in plain words."""
    try:
        ops = declared_operations(obj)
    except Exception as exc:  # a plugin that cannot say what it offers is a broken plugin
        return [f"operations() failed: {type(exc).__name__}"]
    problems: list[str] = []
    seen: set[str] = set()
    for op in ops:
        if not isinstance(op, PluginOperation):
            problems.append("operations() must return PluginOperation values")
            continue
        label = f"operation {op.name!r}"
        if not _OP_NAME.match(op.name):
            problems.append(f"{label}: a name is lower-case letters, digits and underscores")
        if op.name in seen:
            problems.append(f"{label} is defined twice")
        seen.add(op.name)
        if op.permission not in ("read", "mutate"):
            problems.append(f"{label}: permission is read or mutate (a plugin cannot send)")
        if op.mcp not in ("off", "never"):
            problems.append(f"{label}: mcp is off or never (never on by default)")
        for side, model in (("input", op.input), ("output", op.output)):
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                problems.append(f"{label}: {side} must be a pydantic model class")
        if not callable(op.handler):
            problems.append(f"{label}: handler must be an async function")
    return problems


def declared_job_handlers(obj: object) -> list[PluginJobHandler]:
    """The job kinds ``obj.job_handlers()`` returns (none if it has no such method). Raises what
    the method raises; ``check_job_handlers`` turns that into a problem."""
    fn = getattr(obj, "job_handlers", None)
    if not callable(fn):
        return []
    return list(fn())


def check_job_handlers(obj: object) -> list[str]:
    """Problems with the job kinds a plugin offers, in plain words."""
    try:
        handlers = declared_job_handlers(obj)
    except Exception as exc:  # a plugin that cannot say what it offers is a broken plugin
        return [f"job_handlers() failed: {type(exc).__name__}"]
    problems: list[str] = []
    seen: set[str] = set()
    for jh in handlers:
        if not isinstance(jh, PluginJobHandler):
            problems.append("job_handlers() must return PluginJobHandler values")
            continue
        label = f"job kind {jh.kind!r}"
        if not _OP_NAME.match(jh.kind):
            problems.append(f"{label}: a kind is lower-case letters, digits and underscores")
        if jh.kind in seen:
            problems.append(f"{label} is defined twice")
        seen.add(jh.kind)
        if not (isinstance(jh.payload_model, type) and issubclass(jh.payload_model, BaseModel)):
            problems.append(f"{label}: payload_model must be a pydantic model class")
        if jh.result_model is not None and not (
            isinstance(jh.result_model, type) and issubclass(jh.result_model, BaseModel)
        ):
            problems.append(f"{label}: result_model must be a pydantic model class")
        if not callable(jh.handler):
            problems.append(f"{label}: handler must be an async function")
        bad_concurrency = jh.concurrency is not None and (
            not isinstance(jh.concurrency, int) or jh.concurrency < 1
        )
        if bad_concurrency:
            problems.append(f"{label}: concurrency must be a positive integer")
    return problems


def declared_pipeline_stages(obj: object) -> list[PluginStage]:
    """The stages ``obj.pipeline_stages()`` returns (none if it has no such method). Raises what
    the method raises; ``check_pipeline_stages`` turns that into a problem."""
    fn = getattr(obj, "pipeline_stages", None)
    if not callable(fn):
        return []
    return list(fn())


def check_pipeline_stages(obj: object) -> list[str]:
    """Problems with the pipeline stages a plugin offers, in plain words."""
    try:
        stages = declared_pipeline_stages(obj)
    except Exception as exc:  # a plugin that cannot say what it offers is a broken plugin
        return [f"pipeline_stages() failed: {type(exc).__name__}"]
    problems: list[str] = []
    seen: set[str] = set()
    for stage in stages:
        if not isinstance(stage, PluginStage):
            problems.append("pipeline_stages() must return PluginStage values")
            continue
        label = f"pipeline stage {stage.name!r}"
        if not _OP_NAME.match(stage.name):
            problems.append(f"{label}: a name is lower-case letters, digits and underscores")
        if stage.name in seen:
            problems.append(f"{label} is defined twice")
        seen.add(stage.name)
        if not (isinstance(stage.version, str) and stage.version):
            problems.append(f"{label}: version must be a non-empty string")
        if not callable(stage.handler):
            problems.append(f"{label}: handler must be an async function")
    return problems
