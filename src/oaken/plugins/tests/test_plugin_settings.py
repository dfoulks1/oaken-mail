# SPDX-License-Identifier: Apache-2.0
"""What a person can say about a plugin in its [plugins.<name>] table: the ``capabilities``
allow-list, ``enabled`` and the plugin's own default for it, and trying a config before saving."""

from oaken.core.settings import Settings, SyncSettings
from oaken.plugins.base import inferred_capabilities
from oaken.plugins.tests.conftest import Installer
from oaken.plugins.tests.test_host import make_host

PROVIDER = """
from oaken.plugins.base import BaseProvider
from oaken.plugins.contracts import Health

class Full(BaseProvider):
    version = "1"
    starts = 0
    stops = 0
    async def start(self, ctx):
        await super().start(ctx)
        type(self).starts += 1
        if ctx.config.get("break"):
            raise ValueError("cannot reach the server")
    async def stop(self):
        type(self).stops += 1
    async def health(self):
        return Health(self.ctx.config.get("host") != "down", "server is down")
    async def authenticate(self, ctx): ...
    async def set_flags(self, provider_ids, add, remove): ...
    async def set_password(self, password): ...
    async def pull(self, cursor):
        yield {"messages": [], "next_cursor": "1"}

class Optional(BaseProvider):
    enabled_default = False
    async def pull(self, cursor):
        yield {"next_cursor": "1"}
"""

ENTRIES = [("oaken.providers", "full", "Full"), ("oaken.providers", "optin", "Optional")]


def _host(install_plugin: Installer, name: str, **tables: dict[str, object]):  # type: ignore[no-untyped-def]
    install_plugin(name, PROVIDER, ENTRIES)
    return make_host(Settings(sync=SyncSettings(provider="full"), plugins=tables))[0]


async def test_an_allow_list_narrows_what_the_plugin_may_do(install_plugin: Installer) -> None:
    host = _host(install_plugin, "plug_allow", full={"capabilities": ["sync.pull"], "x": 1})
    await host.start()
    try:
        st = next(s for s in host.statuses() if s.name == "full")
        assert st.capabilities == {"sync.pull"} and st.allowed == {"sync.pull"}
        assert len(host.providers_with("sync.pull")) == 1
        assert host.providers_with("flags.write") == []
        instance = host.provider("full")
        assert inferred_capabilities(instance, "provider") == {"sync.pull"}
        assert instance.ctx.config == {"x": 1}  # the plugin never sees the host's keys
        assert instance.operations() == []  # no `auth`
    finally:
        await host.stop()


async def test_no_allow_list_means_everything_the_plugin_implements(
    install_plugin: Installer,
) -> None:
    host = _host(install_plugin, "plug_noallow")
    await host.start()
    try:
        st = next(s for s in host.statuses() if s.name == "full")
        assert st.capabilities == {"auth", "sync.pull", "flags.write"} and st.allowed is None
        assert {o.name for o in host.provider("full").operations()} == {"auth"}
    finally:
        await host.stop()


async def test_a_bad_allow_list_is_a_clear_start_failure(install_plugin: Installer) -> None:
    install_plugin("plug_badallow", PROVIDER, ENTRIES)
    for bad, why in [
        (["sync.pul"], "unknown capability 'sync.pul'"),
        (["auth"], "nothing to do"),
        ([], "nothing to do"),
        ("sync.pull", "must be a list"),
    ]:
        settings = Settings(
            sync=SyncSettings(provider="full"), plugins={"full": {"capabilities": bad}}
        )
        host = make_host(settings)[0]
        await host.start()
        try:
            st = next(s for s in host.statuses() if s.name == "full")
            assert not st.started and why in st.detail, (bad, st.detail)
        finally:
            await host.stop()


async def test_naming_what_the_plugin_lacks_cannot_widen_it(install_plugin: Installer) -> None:
    host = _host(install_plugin, "plug_widen", full={"capabilities": ["sync.pull", "send"]})
    await host.start()
    try:
        st = next(s for s in host.statuses() if s.name == "full")
        assert st.capabilities == {"sync.pull"}  # Full never implemented send
        assert host.providers_with("send") == []
    finally:
        await host.stop()


async def test_a_plugin_that_ships_switched_off_stays_off_until_asked(
    install_plugin: Installer,
) -> None:
    host = _host(install_plugin, "plug_optin")
    await host.start()
    try:
        st = next(s for s in host.statuses() if s.name == "optin")
        assert (st.enabled, st.enabled_default, st.started) == (False, False, False)
        assert st.detail == "disabled"
        await host.check_health()
        assert not next(s for s in host.statuses() if s.name == "optin").started
        await host.enable("optin")
        st = next(s for s in host.statuses() if s.name == "optin")
        assert (st.enabled, st.started) == (True, True)
        assert next(s for s in host.statuses() if s.name == "full").enabled_default is True
    finally:
        await host.stop()


async def test_the_table_beats_the_plugins_own_default(install_plugin: Installer) -> None:
    host = _host(install_plugin, "plug_table", optin={"enabled": True}, full={"enabled": False})
    await host.start()
    try:
        by = {s.name: s for s in host.statuses()}
        assert by["optin"].started and by["optin"].enabled
        assert not by["full"].started and not by["full"].enabled
        await host.enable("full")  # asking at runtime does not override the file
        assert not next(s for s in host.statuses() if s.name == "full").started
    finally:
        await host.stop()


async def test_a_probe_starts_a_throwaway_and_leaves_the_running_plugin_alone(
    install_plugin: Installer,
) -> None:
    host = _host(install_plugin, "plug_probe", full={"host": "up"})
    await host.start()
    try:
        from plug_probe import Full  # type: ignore[import-not-found]

        before = (Full.starts, Full.stops)
        [ok] = await host.probe("full", {"host": "up"})
        [down] = await host.probe("full", {"host": "down"})
        [broke] = await host.probe("full", {"break": True})
        assert ok.ok
        assert not down.ok and down.detail == "server is down"
        assert not broke.ok and "cannot reach the server" in broke.detail
        assert (Full.starts - before[0], Full.stops - before[1]) == (
            3,
            2,
        )  # a failed start is not stopped
        assert next(s for s in host.statuses() if s.name == "full").healthy  # untouched
        [bad] = await host.probe("full", {"capabilities": ["nope"]})
        assert not bad.ok and "unknown capability" in bad.detail
    finally:
        await host.stop()


def test_check_config_finds_problems_without_starting_anything(
    install_plugin: Installer,
) -> None:
    host = _host(install_plugin, "plug_check")
    import asyncio

    asyncio.run(host.load())
    assert host.check_config("full", {"capabilities": ["sync.pull"]}) == []
    [problem] = host.check_config("full", {"capabilities": ["auth"]})
    assert problem.startswith("provider full:") and "nothing to do" in problem
