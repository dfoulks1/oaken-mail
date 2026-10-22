# SPDX-License-Identifier: Apache-2.0
"""Register the job kinds healthy plugins offer on the job runner.

A plugin defines ``job_handlers()`` returning ``PluginJobHandler`` values; core registers each on
the ``JobRunner`` as ``plugin.<name>.<kind>`` so two plugins cannot collide and a plugin kind is
always distinguishable from a core one. The plugin's handler is given a plugin-scoped
``PluginJobContext`` (its own config, the clock, a logger — no database, no queue, no ``Job``
internals) and its validated payload; its result, if any, is validated against ``result_model``
exactly as for a core kind. A plugin failure is reduced to its exception type, because it could
quote mail — the same rule the operation bridge uses.
"""

from typing import Any

from pydantic import BaseModel

from oaken.core.errors import OakenError, PluginError
from oaken.core.jobs import Job, JobRunner
from oaken.core.logging import get_logger
from oaken.plugins.contracts import PluginJobHandler
from oaken.plugins.host import OfferedJobHandler, PluginHost

log = get_logger("oaken.plugins")

PLUGIN_JOB_PREFIX = "plugin"


def namespaced_kind(plugin: str, kind: str) -> str:
    """The job kind a plugin kind is registered under: ``plugin.<name>.<kind>``."""
    return f"{PLUGIN_JOB_PREFIX}.{plugin}.{kind}"


def _adapt(host: PluginHost, offered: OfferedJobHandler) -> tuple[str, PluginJobHandler, Any]:
    """The namespaced kind, the declaration, and a runner handler wrapping the plugin's handler."""
    plugin, decl = offered.plugin, offered.declaration
    full = namespaced_kind(plugin, decl.kind)

    async def handler(payload: BaseModel, _job: Job) -> BaseModel | None:
        ctx = host.job_context(plugin, full)
        try:
            result = await decl.handler(ctx, payload)
        except OakenError:
            raise
        except Exception as exc:  # a plugin error could quote mail: keep only its type
            raise PluginError(f"{full} failed ({type(exc).__name__})") from None
        if decl.result_model is not None and result is not None:
            return decl.result_model.model_validate(result)
        return result if isinstance(result, BaseModel) else None

    return full, decl, handler


def register_plugin_jobs(host: PluginHost, runner: JobRunner) -> list[str]:
    """Register every healthy plugin's job kinds on ``runner``. Returns the namespaced kinds
    registered. A declaration that collides with an already-registered kind is skipped and logged,
    never raised, so one bad plugin cannot stop core's jobs from starting."""
    registered: list[str] = []
    for offered in host.offered_job_handlers():
        full, decl, handler = _adapt(host, offered)
        try:
            runner.register(
                full,
                decl.payload_model,
                handler,
                concurrency=decl.concurrency,
                result_model=decl.result_model,
            )
        except ValueError as exc:  # already registered (a collision): log and move on
            log.error("plugin job kind refused", kind=full, problem=str(exc))
            continue
        registered.append(full)
    return registered
