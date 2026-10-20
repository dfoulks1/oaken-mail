# SPDX-License-Identifier: Apache-2.0
"""Workspaces: named places AI work happens, kept apart.

A ``secure`` workspace only ever uses a backend that runs on this machine; an ``insecure`` one may
use a remote service. Their LLM caches and usage are separate, so nothing computed in one is served
to the other. Enforcement is by the cache key and by ``check_backend``, not by the interface.
"""

from dataclasses import dataclass
from typing import Any

from oaken.core.errors import ConfigError, PermissionDenied
from oaken.core.ids import AccountId
from oaken.core.settings import Settings

DEFAULT_WORKSPACE = "default"


@dataclass(frozen=True)
class Workspace:
    name: str
    trust: str  # "secure" | "insecure"
    llm_backend: str  # entry-point name of the LLM backend this workspace uses

    @property
    def secure(self) -> bool:
        return self.trust == "secure"


class Workspaces:
    def __init__(self, settings: Settings) -> None:
        self._ws: dict[str, Workspace] = {}
        for name, cfg in settings.workspaces.items():
            self._ws[name] = Workspace(name, cfg.trust, cfg.llm_backend or settings.ai.llm_backend)
        self._ws.setdefault(
            DEFAULT_WORKSPACE, Workspace(DEFAULT_WORKSPACE, "secure", settings.ai.llm_backend)
        )

    def names(self) -> list[str]:
        return sorted(self._ws)

    def get(self, name: str | None = None) -> Workspace:
        key = name or DEFAULT_WORKSPACE
        try:
            return self._ws[key]
        except KeyError:
            raise ConfigError(
                f"no workspace named {key!r} (see [workspaces] in config.toml)"
            ) from None

    async def for_account(self, raw: Any, account_id: AccountId) -> Workspace:
        """The workspace an account's mail is processed in: its non-secret ``workspace`` setting,
        else ``default``."""
        account = await raw.get_account(account_id)
        return self.get(account.config.get("workspace"))


def check_backend(ws: Workspace, backend: object) -> None:
    """Refuse to hand a ``secure`` workspace's mail to a backend that is not local. A backend says
    where it runs with ``locality`` (``"local"`` or ``"remote"``); one that does not is remote."""
    where = getattr(backend, "locality", "remote")
    if ws.secure and where != "local":
        raise PermissionDenied(
            f"workspace {ws.name!r} is secure and {ws.llm_backend!r} is not a local backend"
        )
