# SPDX-License-Identifier: Apache-2.0
"""One provider instance per account (prompt 12b).

A provider plugin is a class; an account is a row in ``raw.db`` naming one (``accounts.provider``)
and holding that account's own non-secret settings. This starts a separate instance of the plugin
for every account, so three Gmail accounts and a Fastmail one each have their own credentials, state
and health. An account whose provider cannot start is reported with its reason and never stops the
others.
"""

from oaken.core.errors import NotFound, OakenError, PluginError
from oaken.core.ids import AccountId
from oaken.core.logging import get_logger
from oaken.core.rawstore import RawStore
from oaken.plugins.authorize import Authorizations
from oaken.plugins.contracts import Provider
from oaken.plugins.host import PluginHost

log = get_logger("oaken.accounts")


class AccountProviders:
    def __init__(self, host: PluginHost, raw: RawStore) -> None:
        self.host = host
        self.raw = raw
        self._names: dict[str, str] = {}  # account id -> the host's instance name
        self._problems: dict[str, str] = {}  # account id -> why it has no provider
        self.authorizations = Authorizations()  # interactive consent flows in progress

    async def start(self) -> dict[str, str]:
        """Start a provider for every account that has none yet (call again after adding an
        account). Returns the accounts that could not get one, with the reason."""
        for acct in await self.raw.list_accounts():
            if acct.id in self._names:
                continue
            try:
                self._names[acct.id] = await self.host.start_account_instance(
                    "provider", acct.provider, acct.id, acct.config
                )
                self._problems.pop(acct.id, None)
            except OakenError as exc:  # no such plugin installed: say so, carry on
                self._problems[acct.id] = str(exc)
                log.warning("account has no provider", account_id=acct.id, reason=str(exc))
        return dict(self._problems)

    def get(self, account_id: str, *, allow_unhealthy: bool = False) -> Provider:
        """The started, healthy provider of this account, or ``PluginError`` saying why not.
        ``allow_unhealthy`` also returns one that started but reports itself unhealthy, which is
        how an account whose credential is missing or dead is authorized again."""
        if account_id in self._problems:
            raise PluginError(f"account {account_id!r}: {self._problems[account_id]}")
        name = self._names.get(account_id)
        if name is None:
            raise NotFound(f"no provider was started for account {account_id!r}")
        if allow_unhealthy:
            return self.host.get_started("provider", name)  # type: ignore[no-any-return]
        return self.host.get("provider", name)  # type: ignore[no-any-return]

    def problem(self, account_id: str) -> str | None:
        """Why this account cannot sync right now, or None if its provider is healthy."""
        try:
            self.get(account_id)
        except OakenError as exc:
            return str(exc)
        return None

    def problems(self) -> dict[str, str]:
        return {a: p for a in sorted({*self._names, *self._problems}) if (p := self.problem(a))}

    def account_ids(self) -> list[AccountId]:
        return [AccountId(a) for a in sorted(self._names)]
